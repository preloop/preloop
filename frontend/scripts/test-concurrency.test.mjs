import assert from 'node:assert/strict';
import { test } from 'node:test';
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
