// Desktop capability advertised on the OpenClaw presence envelope.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  PreloopOpenClawPlugin,
  readDesktopCapability,
} from "../dist/index.js";

const SECRET = "synthetic-vnc-secret";

function writeManifest(dir, host = "127.0.0.1") {
  const manifest = path.join(dir, "desktop.json");
  fs.writeFileSync(
    manifest,
    JSON.stringify({
      display: ":99",
      vnc: { host, port: 5900, auth: "rfbauth", password: SECRET },
      browser: "chromium",
      password_file: path.join(dir, "vncpasswd"),
    }),
  );
  fs.writeFileSync(path.join(dir, "vncpasswd"), SECRET);
  return manifest;
}

test("capabilities report vnc for a loopback desktop manifest", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-desktop-"));
  const manifest = writeManifest(dir);
  const previous = process.env.PRELOOP_DESKTOP_FILE;
  process.env.PRELOOP_DESKTOP_FILE = manifest;
  try {
    const plugin = new PreloopOpenClawPlugin();
    const capabilities = plugin.capabilities();
    assert.equal(capabilities.desktop, "vnc");
    assert.equal(capabilities.desktop_display, ":99");
    const serialized = JSON.stringify(capabilities);
    assert.equal(serialized.includes(SECRET), false);
    assert.equal(serialized.includes("vncpasswd"), false);
    assert.equal(serialized.includes("rfbauth"), false);
    assert.equal(serialized.includes("5900"), false);
    assert.deepEqual(readDesktopCapability(), {
      desktop: "vnc",
      desktop_display: ":99",
    });
  } finally {
    if (previous === undefined) {
      delete process.env.PRELOOP_DESKTOP_FILE;
    } else {
      process.env.PRELOOP_DESKTOP_FILE = previous;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("capabilities report none when the desktop manifest is missing", () => {
  const previous = process.env.PRELOOP_DESKTOP_FILE;
  process.env.PRELOOP_DESKTOP_FILE = path.join(
    os.tmpdir(),
    "preloop-desktop-missing.json",
  );
  try {
    const capabilities = new PreloopOpenClawPlugin().capabilities();
    assert.equal(capabilities.desktop, "none");
    assert.equal(capabilities.desktop_display, null);
  } finally {
    if (previous === undefined) {
      delete process.env.PRELOOP_DESKTOP_FILE;
    } else {
      process.env.PRELOOP_DESKTOP_FILE = previous;
    }
  }
});

test("capabilities report none when vnc host is not loopback", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-desktop-"));
  const manifest = writeManifest(dir, "10.0.0.8");
  const previous = process.env.PRELOOP_DESKTOP_FILE;
  process.env.PRELOOP_DESKTOP_FILE = manifest;
  try {
    const capabilities = new PreloopOpenClawPlugin().capabilities();
    assert.equal(capabilities.desktop, "none");
    assert.equal(capabilities.desktop_display, null);
    assert.equal(JSON.stringify(capabilities).includes(SECRET), false);
  } finally {
    if (previous === undefined) {
      delete process.env.PRELOOP_DESKTOP_FILE;
    } else {
      process.env.PRELOOP_DESKTOP_FILE = previous;
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
