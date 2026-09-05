// CLI entry detection. npm installs the bin as a symlink
// (bin/preloop-claude-plugin -> dist/index.js) and Node resolves
// import.meta.url to the realpath while process.argv[1] keeps the symlink,
// so the entry guard must realpath argv[1] before comparing. Regression for
// the sidecar exiting 0 without listening when started through the npm bin.
import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const entry = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "dist",
  "index.js",
);

const validConfig = {
  enabled: true,
  protocol: "preloop.agent_control.v1",
  runtime: "claude_code",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "claude-code-1",
};

function writeTempConfig() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preloop-claude-cli-"));
  const file = path.join(dir, "preloop-control.json");
  fs.writeFileSync(file, JSON.stringify(validConfig));
  return { dir, file };
}

test("verify works when invoked directly", () => {
  const { file } = writeTempConfig();
  const result = spawnSync(process.execPath, [entry, "verify", "--config", file], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /verified/);
});

test("verify works when invoked through a bin-style symlink", () => {
  const { dir, file } = writeTempConfig();
  const link = path.join(dir, "preloop-claude-plugin");
  fs.symlinkSync(entry, link);
  const result = spawnSync(process.execPath, [link, "verify", "--config", file], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(
    result.stdout,
    /verified/,
    "symlinked bin must dispatch the CLI instead of exiting silently",
  );
});
