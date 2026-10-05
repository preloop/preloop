import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const braces = require('../vendor/braces');

test('ordinary brace expansion is unchanged', () => {
  assert.deepEqual(braces.expand('{a,b}'), ['a', 'b']);
  assert.deepEqual(braces('a/{b,c}/d'), ['a/(b|c)/d']);
});

test('deeply nested braces stay literal instead of overflowing the stack', () => {
  const pattern = '{'.repeat(4000) + 'a' + '}'.repeat(4000);
  assert.deepEqual(braces(pattern), [pattern]);
  assert.deepEqual(braces.expand(pattern), [pattern]);
  assert.equal(braces.compile(pattern), pattern);
});
