import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';

const require = createRequire(import.meta.url);
const braces = require('braces');
const resolved = require.resolve('braces');

test('micromatch resolves the patched braces package', () => {
  assert.ok(
    resolved.includes(`${path.sep}vendor${path.sep}braces${path.sep}`),
    resolved,
  );
});

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
