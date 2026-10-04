import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  isShippedFile,
  resolveVendorRequest,
  SHOELACE_VENDOR_BASE,
} from '../vite-plugin-shoelace-vendor.ts';

const frontendDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..'
);
const packageCdn = path.join(
  frontendDir,
  'node_modules/@shoelace-style/shoelace/cdn'
);

function fixtureDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'shoelace-vendor-'));
  fs.mkdirSync(path.join(dir, 'themes'));
  fs.writeFileSync(path.join(dir, 'themes/light.css'), ':root{}');
  fs.mkdirSync(path.join(dir, 'react'));
  fs.writeFileSync(path.join(dir, 'react/index.js'), '');
  return dir;
}

test('index.html loads Shoelace from this origin, never a CDN', () => {
  const html = fs.readFileSync(path.join(frontendDir, 'index.html'), 'utf8');
  assert.doesNotMatch(html, /cdn\.jsdelivr\.net|unpkg\.com/);
  for (const asset of [
    'themes/light.css',
    'themes/dark.css',
    'shoelace-autoloader.js',
  ]) {
    assert.ok(
      html.includes(`${SHOELACE_VENDOR_BASE}${asset}`),
      `index.html should reference ${SHOELACE_VENDOR_BASE}${asset}`
    );
  }
});

test('every file index.html references exists in the installed package', () => {
  for (const asset of [
    'themes/light.css',
    'themes/dark.css',
    'shoelace-autoloader.js',
    'assets/icons/gear.svg',
  ]) {
    const file = resolveVendorRequest(
      packageCdn,
      `${SHOELACE_VENDOR_BASE}${asset}`
    );
    assert.ok(file && fs.existsSync(file), `${asset} is shipped`);
  }
});

test('resolves shipped files and ignores query strings', () => {
  const dir = fixtureDir();
  assert.equal(
    resolveVendorRequest(dir, `${SHOELACE_VENDOR_BASE}themes/light.css?v=1`),
    path.join(dir, 'themes/light.css')
  );
});

test('refuses paths outside the base, the package, or the shipped set', () => {
  const dir = fixtureDir();
  assert.equal(resolveVendorRequest(dir, '/assets/app.js'), null);
  assert.equal(
    resolveVendorRequest(dir, `${SHOELACE_VENDOR_BASE}..%2Fpackage.json`),
    null
  );
  assert.equal(
    resolveVendorRequest(
      dir,
      `${SHOELACE_VENDOR_BASE}%2e%2e/%2e%2e/etc/passwd`
    ),
    null
  );
  assert.equal(
    resolveVendorRequest(dir, `${SHOELACE_VENDOR_BASE}react/index.js`),
    null
  );
  assert.equal(
    resolveVendorRequest(dir, `${SHOELACE_VENDOR_BASE}%E0%A4%A`),
    null
  );
});

test('type declarations and source maps are not shipped', () => {
  assert.equal(isShippedFile('components/button/button.js'), true);
  assert.equal(isShippedFile('components/button/button.d.ts'), false);
  assert.equal(isShippedFile('chunks/chunk.ABC.js.map'), false);
  assert.equal(isShippedFile('custom-elements.json'), false);
});
