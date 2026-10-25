import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src');
// The single definition of the REST prefix. Tests and test helpers assert
// on literal URLs and are excluded below.
const ALLOWED = new Set(['api-path.ts']);

function sourceFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name === 'test-helpers' ? [] : sourceFiles(full);
    }
    return /\.ts$/.test(entry.name) && !/\.test\.ts$/.test(entry.name)
      ? [full]
      : [];
  });
}

test('REST URLs are built from REST_API_PREFIX, not a literal /api/v1', () => {
  const offenders = [];
  for (const file of sourceFiles(SRC)) {
    const rel = path.relative(SRC, file);
    if (ALLOWED.has(rel)) continue;
    readFileSync(file, 'utf8')
      .split('\n')
      .forEach((line, index) => {
        if (/^\s*(\*|\/\/)/.test(line)) return;
        if (/['"`]\/api\/v1\b/.test(line)) offenders.push(`${rel}:${index + 1}`);
      });
  }
  assert.deepEqual(offenders, []);
});
