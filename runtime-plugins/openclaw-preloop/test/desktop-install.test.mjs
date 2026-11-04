// desktop.install over Agent Control (#1566).
import assert from "node:assert/strict";
import { test } from "node:test";

import { PreloopOpenClawPlugin, runDesktopEnable } from "../dist/index.js";

const command = {
  type: "command",
  name: "desktop.install",
  message_id: "req-1",
  payload: { request_id: "req-1", managed_agent_id: "agent-1" },
};

test("desktop.install runs preloop desktop enable and reports ok", async () => {
  const calls = [];
  const plugin = new PreloopOpenClawPlugin();
  const envelope = await plugin.desktopInstallEnvelope(command, async (argv) => {
    calls.push(argv);
    return { code: 0, output: "Enabled" };
  });
  assert.deepEqual(calls, [["preloop", "desktop", "enable", "--agent", "agent-1"]]);
  assert.deepEqual(envelope, {
    type: "status",
    name: "command_result",
    message_id: "req-1",
    payload: {
      command_id: "req-1",
      status: "completed",
      command: "desktop.install",
      desktop_install: { ok: true },
    },
  });
});

test("desktop.install maps CLI failures to the contract's codes", async () => {
  for (const [output, error] of [
    ["Error: desktop_unsupported_distro", "desktop_unsupported_distro"],
    ["Error: desktop_privilege_required", "desktop_privilege_required"],
    ["Error: desktop_unsupported_os", "desktop_unsupported_os"],
    ["boom", "desktop_failed"],
  ]) {
    assert.deepEqual(
      await runDesktopEnable("agent-1", async () => ({ code: 1, output })),
      { ok: false, error },
    );
  }
  assert.deepEqual(
    await runDesktopEnable("agent-1", async () => {
      throw new Error("ENOENT");
    }),
    { ok: false, error: "desktop_failed" },
  );
});
