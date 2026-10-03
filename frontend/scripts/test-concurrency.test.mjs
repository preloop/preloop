import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { testConcurrency } from './test-concurrency.mjs';

test('flow containers run one browser test at a time', () => {
  assert.equal(testConcurrency({ FLOW_ID: 'flow', EXECUTION_ID: 'execution' }), 1);
});

test('local and CI runs preserve the test runner default', () => {
  assert.equal(testConcurrency({}), undefined);
  assert.equal(testConcurrency({ FLOW_ID: 'flow' }), undefined);
  assert.equal(testConcurrency({ EXECUTION_ID: 'execution' }), undefined);
});

test('operators can explicitly set the browser concurrency', () => {
  assert.equal(testConcurrency({ PRELOOP_TEST_CONCURRENCY: '2' }), 2);
  assert.equal(testConcurrency({
    FLOW_ID: 'flow', EXECUTION_ID: 'execution', PRELOOP_TEST_CONCURRENCY: '3',
  }), 3);
});

test('invalid overrides fail instead of silently enabling extra workers', () => {
  for (const value of ['', '0', '-1', '1.5', 'many', ' 2 ', 'Infinity', '9007199254740992']) {
    assert.throws(() => testConcurrency({ PRELOOP_TEST_CONCURRENCY: value }),
      /must be a positive integer/);
  }
});

for (const configFile of ['web-test-runner.config.mjs', 'web-test-runner.config.js']) {
  for (const [scenario, variables, expected] of [
    ['local/CI defaults', {}, undefined],
    ['flow container', { FLOW_ID: 'flow', EXECUTION_ID: 'execution' }, 1],
    ['operator override', { PRELOOP_TEST_CONCURRENCY: '2' }, 2],
  ]) {
    test(`${configFile} preserves runnable concurrency for ${scenario}`, () => {
      const env = { ...process.env };
      for (const key of ['FLOW_ID', 'EXECUTION_ID', 'PRELOOP_TEST_CONCURRENCY']) {
        delete env[key];
      }
      Object.assign(env, variables);
      // Load each actual config in a fresh environment and use the runner's
      // real merger: an own property with undefined erases its default and
      // prevents browser sessions from launching.
      const script = `
        import assert from 'node:assert/strict';
        import { createRequire } from 'node:module';
        import path from 'node:path';
        import config from './${configFile}';
        const require = createRequire(import.meta.url);
        const runner = require.resolve('@web/test-runner');
        const { parseConfig } = require(path.join(path.dirname(runner), 'config/parseConfig.js'));
        const options = { files: ['src/utils/debug.test.ts'], port: 8000 };
        const baseline = await parseConfig(options);
        const parsed = await parseConfig({ ...config, ...options });
        assert.ok(Number.isFinite(parsed.config.concurrency));
        assert.ok(parsed.config.concurrency > 0);
        assert.equal(parsed.config.concurrency, ${expected === undefined ? 'baseline.config.concurrency' : expected});
        ${expected === undefined ? "assert.equal(Object.hasOwn(config, 'concurrency'), false);" : ''}
      `;
      const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
        cwd: fileURLToPath(new URL('..', import.meta.url)),
        env,
        encoding: 'utf8',
        timeout: 10000,
      });
      assert.equal(result.status, 0, result.stderr || String(result.error));
    });
  }
}
