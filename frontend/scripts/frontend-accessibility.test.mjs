import test from 'node:test';
import assert from 'node:assert/strict';
import { accessibilityFindings } from './check-frontend-accessibility.mjs';

test('rejects placeholder-only controls and raw low-contrast text', () => {
  assert.equal(accessibilityFindings('<sl-input placeholder="Search"></sl-input>').length, 1);
  assert.equal(accessibilityFindings('<sl-input aria-label=""></sl-input>').length, 1);
  assert.equal(accessibilityFindings('.meta { color: var(--sl-color-neutral-500); }').length, 1);
});
test('keeps non-text colors and accepts programmatic labels', () => {
  assert.deepEqual(accessibilityFindings('background-color: var(--sl-color-neutral-500); border-color: var(--sl-color-neutral-400); <sl-select aria-label="Status"></sl-select>'), []);
  assert.deepEqual(accessibilityFindings('<label for="email">Email</label><input id="email">'), []);
});
