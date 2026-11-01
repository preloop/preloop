import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const lock = JSON.parse(
  readFileSync(new URL("../agents/pi/package-lock.json", import.meta.url), "utf8"),
);

function versionsOf(name) {
  return Object.entries(lock.packages)
    .filter(([path]) => path.endsWith(`node_modules/${name}`))
    .map(([, entry]) => entry.version);
}

function atLeast(version, floor) {
  const a = version.split(".").map(Number);
  const b = floor.split(".").map(Number);
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return true;
}

test("pinned Pi lockfile has no minimatch below 11.0.1 (GHSA-g3hv-75wg-m249)", () => {
  const versions = versionsOf("minimatch");
  assert.ok(versions.length > 0);
  for (const version of versions) {
    assert.ok(atLeast(version, "11.0.1"), `minimatch ${version} is vulnerable`);
  }
});
