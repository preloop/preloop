/**
 * Fail when frontend/src has a TODO or FIXME with no issue reference.
 *
 * A reference is a #N marker on the same line (for example TODO(#123)).
 * Deferred work without one is invisible to the next code health pass.
 */
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const src = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../src'
);
const marker = /\b(TODO|FIXME)\b/;
const issueRef = /#\d+/;

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

const failures = [];
for (const file of await walk(src)) {
  const lines = (await readFile(file, 'utf8')).split('\n');
  lines.forEach((line, index) => {
    if (marker.test(line) && !issueRef.test(line)) {
      const rel = path.relative(src, file);
      failures.push(`${rel}:${index + 1}: ${line.trim()}`);
    }
  });
}

if (failures.length > 0) {
  console.error('TODO/FIXME comments without an issue reference:');
  for (const failure of failures) {
    console.error(`  ${failure}`);
  }
  process.exit(1);
}
