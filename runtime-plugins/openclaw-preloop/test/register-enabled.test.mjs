// Unit tests for register() honouring config.enabled.
// Uses Node's built-in test runner against the built output: run `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { PreloopOpenClawPlugin, register } from "../dist/index.js";

const baseConfig = {
  runtime: "openclaw",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "secret-token",
  runtime_principal_id: "principal-1",
};

const REGISTERED_HOOKS = ["gateway_start", "gateway_stop", "before_tool_call"];

function makeApi(pluginConfig) {
  const hooks = [];
  const logs = [];
  const api = {
    pluginConfig,
    on: (name, handler) => {
      hooks.push({ name, handler });
    },
    logger: {
      info: (message) => logs.push(["info", message]),
      warn: (message) => logs.push(["warn", message]),
      error: (message) => logs.push(["error", message]),
    },
  };
  return { api, hooks, logs };
}

function spyStart() {
  const original = PreloopOpenClawPlugin.prototype.start;
  const calls = [];
  PreloopOpenClawPlugin.prototype.start = function spy(...args) {
    calls.push(args);
    return Promise.resolve();
  };
  return {
    calls,
    restore: () => {
      PreloopOpenClawPlugin.prototype.start = original;
    },
  };
}

test("register with enabled false calls api.on zero times and never connects", () => {
  const { api, hooks, logs } = makeApi({ ...baseConfig, enabled: false });
  const spy = spyStart();
  try {
    register(api);
    assert.equal(hooks.length, 0);
    assert.equal(spy.calls.length, 0);
    assert.deepEqual(logs, [
      [
        "info",
        "Preloop plugin is installed but disabled (config.enabled=false): no Agent Control channel, no tool-call hook",
      ],
    ]);
  } finally {
    spy.restore();
  }
});

test("register with enabled false uses warn when info is absent", () => {
  const hooks = [];
  const logs = [];
  const api = {
    pluginConfig: { ...baseConfig, enabled: false },
    on: (name) => {
      hooks.push(name);
    },
    logger: {
      warn: (message) => logs.push(["warn", message]),
    },
  };
  const spy = spyStart();
  try {
    register(api);
    assert.equal(hooks.length, 0);
    assert.equal(spy.calls.length, 0);
    assert.deepEqual(logs, [
      [
        "warn",
        "Preloop plugin is installed but disabled (config.enabled=false): no Agent Control channel, no tool-call hook",
      ],
    ]);
  } finally {
    spy.restore();
  }
});

for (const enabled of [true, undefined]) {
  const label = enabled === undefined ? "absent" : "true";
  test(`register with enabled ${label} registers the same hooks as before`, () => {
    const pluginConfig =
      enabled === undefined ? { ...baseConfig } : { ...baseConfig, enabled };
    const { api, hooks } = makeApi(pluginConfig);
    const spy = spyStart();
    try {
      register(api);
      assert.deepEqual(
        hooks.map((hook) => hook.name),
        REGISTERED_HOOKS,
      );
      assert.equal(spy.calls.length, 0);
    } finally {
      spy.restore();
    }
  });
}

test("tool_approval_enabled false with enabled absent still starts the channel and registers gateway_start", () => {
  const { api, hooks } = makeApi({
    ...baseConfig,
    tool_approval_enabled: false,
  });
  const spy = spyStart();
  try {
    register(api);
    const names = hooks.map((hook) => hook.name);
    assert.ok(names.includes("gateway_start"));
    assert.deepEqual(names, REGISTERED_HOOKS);
    const gatewayStart = hooks.find((hook) => hook.name === "gateway_start");
    assert.equal(typeof gatewayStart.handler, "function");
    gatewayStart.handler();
    assert.ok(
      spy.calls.length >= 1,
      `expected instance.start, saw ${spy.calls.length} calls`,
    );
  } finally {
    spy.restore();
  }
});
