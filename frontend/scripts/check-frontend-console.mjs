/**
 * Fail when frontend/src calls console.log or console.debug outside the
 * dev-only helper.
 *
 * Production diagnostics go through `src/utils/debug.ts`, which a
 * production build drops. `console.error` and `console.warn` stay, for
 * failures an operator can act on. Test files are not scanned.
 */
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const src = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../src'
);
const allowed = new Set([path.normalize('utils/debug.ts')]);
const call = /console\.(log|debug)\s*\(/;

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue;
      files.push(...(await walk(full)));
    } else if (/\.(ts|js|mjs)$/.test(entry.name)) {
      files.push(full);
    }
  }
  return files;
}

function isTest(rel) {
  return /\.(test|spec)\.[cm]?[jt]s$/.test(rel);
}

const failures = [];
for (const file of await walk(src)) {
  const rel = path.relative(src, file);
  if (isTest(rel) || allowed.has(path.normalize(rel))) continue;
  const lines = (await readFile(file, 'utf8')).split('\n');
  lines.forEach((line, index) => {
    if (call.test(line)) {
      failures.push(`${rel}:${index + 1}: ${line.trim()}`);
    }
  });
}

if (failures.length > 0) {
  console.error(
    'console.log/console.debug outside src/utils/debug.ts:'
  );
  for (const failure of failures) {
    console.error(`  ${failure}`);
  }
  process.exit(1);
}
