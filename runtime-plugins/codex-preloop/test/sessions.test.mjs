// SessionManager routing against a fake Codex client (no SDK, no network).
import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionManager, threadOptionsFor } from "../dist/sessions.js";

const baseConfig = {
  runtime: "codex",
  control_ws_url: "wss://example.preloop.ai/api/v1/agents/control/ws",
  bearer_token: "agt_secret",
  runtime_principal_id: "codex-1",
  workspace_root: "/tmp/workspace",
  codex_model: "gpt-test",
};

function makeFakeFactory(state) {
  return () => ({
    startThread(options) {
      const id = `thread-${state.threads.length + 1}`;
      const thread = makeThread(state, id, options);
      state.starts.push({ id, options });
      return thread;
    },
    resumeThread(id, options) {
      state.resumes.push({ id, options });
      return makeThread(state, id, options);
    },
  });
}

function makeThread(state, id, options) {
  const thread = {
    id,
    options,
    runs: 0,
    async run(input, runOptions) {
      thread.runs += 1;
      state.runs.push({ id, input, options });
      if (runOptions?.signal?.aborted) {
        throw abortError();
      }
      return {
        finalResponse: `echo: ${input}`,
        usage: { input_tokens: 3, output_tokens: 5, cached_input_tokens: 0, cache_write_input_tokens: 0, reasoning_output_tokens: 1 },
      };
    },
  };
  state.threads.push(thread);
  return thread;
}

function abortError() {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

function makeManager(config = baseConfig) {
  const state = { threads: [], starts: [], resumes: [], runs: [] };
  const manager = new SessionManager(config, makeFakeFactory(state));
  return { manager, state };
}

test("start_new_session starts a thread in workspace_root and returns the reply", async () => {
  const { manager, state } = makeManager();
  const outcome = await manager.sendMessage({
    text: "hello",
    startNewSession: true,
  });
  assert.equal(outcome.reply_text, "echo: hello");
  assert.equal(outcome.session_id, "thread-1");
  assert.equal(outcome.usage.output_tokens, 5);
  assert.equal(state.starts.length, 1);
  assert.equal(state.starts[0].options.workingDirectory, "/tmp/workspace");
  assert.equal(state.starts[0].options.sandboxMode, "workspace-write");
  assert.equal(state.starts[0].options.skipGitRepoCheck, true);
  assert.equal(state.starts[0].options.model, "gpt-test");
  assert.equal(state.starts[0].options.approvalPolicy, undefined);
  manager.stop();
});

test("a second message to the same session id runs on the same thread", async () => {
  const { manager, state } = makeManager();
  const first = await manager.sendMessage({ text: "one", startNewSession: true });
  const second = await manager.sendMessage({
    text: "two",
    targetSessionId: first.session_id,
  });
  assert.equal(second.reply_text, "echo: two");
  assert.equal(second.session_id, first.session_id);
  assert.equal(state.starts.length, 1);
  assert.equal(state.resumes.length, 0);
  assert.equal(state.runs.length, 2);
  manager.stop();
});

test("an unknown session id is resumed", async () => {
  const { manager, state } = makeManager();
  const outcome = await manager.sendMessage({
    text: "resume me",
    targetSessionId: "persisted-abc",
  });
  assert.equal(outcome.reply_text, "echo: resume me");
  assert.equal(outcome.session_id, "persisted-abc");
  assert.equal(state.starts.length, 0);
  assert.equal(state.resumes.length, 1);
  assert.equal(state.resumes[0].id, "persisted-abc");
  assert.equal(state.resumes[0].options.workingDirectory, "/tmp/workspace");
  manager.stop();
});

test("cwd overrides workspace_root", async () => {
  const { manager, state } = makeManager();
  await manager.sendMessage({
    text: "there",
    startNewSession: true,
    cwd: "/tmp/other",
  });
  assert.equal(state.starts[0].options.workingDirectory, "/tmp/other");
  manager.stop();
});

test("danger-full-access is refused unless codex_allow_full_access is set", () => {
  assert.throws(
    () =>
      threadOptionsFor({
        ...baseConfig,
        codex_sandbox_mode: "danger-full-access",
      }),
    /codex_allow_full_access/,
  );
  const allowed = threadOptionsFor({
    ...baseConfig,
    codex_sandbox_mode: "danger-full-access",
    codex_allow_full_access: true,
  });
  assert.equal(allowed.sandboxMode, "danger-full-access");
  const readOnly = threadOptionsFor({
    ...baseConfig,
    codex_sandbox_mode: "read-only",
  });
  assert.equal(readOnly.sandboxMode, "read-only");
});

test("interrupt aborts a running turn and the outcome is stopped", async () => {
  let notifyStarted;
  const started = new Promise((resolve) => {
    notifyStarted = resolve;
  });
  const state = { runs: 0 };
  const factory = () => ({
    startThread() {
      return {
        id: "thread-live",
        run(_input, options) {
          state.runs += 1;
          notifyStarted();
          return new Promise((_resolve, reject) => {
            const fail = () => reject(abortError());
            if (options?.signal?.aborted) {
              fail();
              return;
            }
            options?.signal?.addEventListener("abort", fail, { once: true });
          });
        },
      };
    },
    resumeThread() {
      throw new Error("unexpected resume");
    },
  });
  const manager = new SessionManager(baseConfig, factory);
  const pending = manager.sendMessage({ text: "go", startNewSession: true });
  await started;
  await manager.interrupt("thread-live");
  const outcome = await pending;
  assert.equal(outcome.stopped, true);
  assert.equal(outcome.reply_text, "");
  assert.equal(outcome.session_id, "thread-live");
  assert.equal(state.runs, 1);
  manager.stop();
});

test("a second turn waits until the in-flight run finishes", async () => {
  let releaseFirst;
  const gate = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  let runs = 0;
  const factory = () => ({
    startThread() {
      return {
        id: "thread-serial",
        async run(input) {
          runs += 1;
          if (runs === 1) {
            await gate;
          }
          return { finalResponse: `echo: ${input}`, usage: null };
        },
      };
    },
    resumeThread() {
      throw new Error("unexpected resume");
    },
  });
  const manager = new SessionManager(baseConfig, factory);
  const first = manager.sendMessage({ text: "one", startNewSession: true });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const second = manager.sendMessage({
    text: "two",
    targetSessionId: "thread-serial",
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(runs, 1);
  releaseFirst();
  assert.equal((await first).reply_text, "echo: one");
  assert.equal((await second).reply_text, "echo: two");
  assert.equal(runs, 2);
  manager.stop();
});

test("interrupt with no running turn reports the honest limitation", async () => {
  const { manager } = makeManager();
  await assert.rejects(
    () => manager.interrupt("some-tui-session"),
    /owned by the sidecar/,
  );
  manager.stop();
});

test("a hung turn rejects after turn_timeout_ms", async () => {
  const factory = () => ({
    startThread() {
      return {
        id: "thread-hung",
        run() {
          return new Promise(() => {});
        },
      };
    },
    resumeThread() {
      throw new Error("unexpected resume");
    },
  });
  const manager = new SessionManager(
    { ...baseConfig, turn_timeout_ms: 40 },
    factory,
  );
  await assert.rejects(
    () => manager.sendMessage({ text: "never", startNewSession: true }),
    /timed out after 40ms/,
  );
  manager.stop();
});

test("employee exact-command interrupt never aborts another task", async () => {
  const controllers = new Map();
  let next = 0;
  const manager = new SessionManager(baseConfig, () => ({
    startThread() {
      const id = `owned-${++next}`;
      return { id, run: (_text, options) => new Promise((_resolve, reject) => {
        controllers.set(id, options.signal);
        options.signal.addEventListener("abort", () => reject(abortError()));
      })};
    }, resumeThread() { throw new Error("unexpected resume"); },
  }));
  const first = manager.sendMessage({text:"first", commandId:"command-one", startNewSession:true});
  const second = manager.sendMessage({text:"second", commandId:"command-two", startNewSession:true});
  while (controllers.size < 2) await new Promise(resolve => setTimeout(resolve, 1));
  await manager.interrupt(undefined, "command-one");
  assert.equal((await first).stopped, true);
  assert.equal(controllers.get("owned-2").aborted, false);
  await assert.rejects(manager.interrupt(undefined, "forged-command"), /owned/);
  await manager.interrupt(undefined, "command-two");
  await second;
});

test("employee execution uses scoped gateway and bounded prompt", async () => {
  const configs = [];
  const state = { threads: [], starts: [], resumes: [], runs: [] };
  const manager = new SessionManager(baseConfig, config => {
    configs.push(config);
    return makeFakeFactory(state)();
  });
  await manager.sendMessage({text:"hello", startNewSession:true, metadata:{
    gateway:{api_key:"scoped-example", base_url:"https://example.com/openai/v1", model:"openai/example"},
    run_limits:{max_history_chars:100, timeout_seconds:10},
  }});
  assert.equal(configs[0].codex_gateway_api_key, "scoped-example");
  assert.equal(state.starts[0].options.model, "openai/example");
  await assert.rejects(manager.sendMessage({text:"too much context", startNewSession:true,
    metadata:{run_limits:{max_history_chars:2}}}), /context limit/);
});

test("spawn env registers an SDK session and records the final alias", async () => {
  const fs = await import("node:fs/promises");
  const os = await import("node:os");
  const path = await import("node:path");
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "preloop-1552-"));
  const original = { home: process.env.HOME, alias: process.env.PRELOOP_SESSION_ALIAS, parent: process.env.PRELOOP_PARENT_SESSION_ID, fetch: globalThis.fetch };
  const requests = [];
  process.env.HOME = home;
  process.env.PRELOOP_SESSION_ALIAS = "brief";
  process.env.PRELOOP_PARENT_SESSION_ID = "11111111-2222-3333-4444-555555555555";
  globalThis.fetch = async (url, options) => {
    requests.push({url: String(url), body: JSON.parse(options.body)});
    return new Response(JSON.stringify({runtime_session_id:"99999999-2222-3333-4444-555555555555", alias:"brief-2", parent_session_id:process.env.PRELOOP_PARENT_SESSION_ID}), {status:200});
  };
  await fs.mkdir(path.join(home, ".preloop/sessions"), {recursive:true});
  await fs.writeFile(path.join(home, ".preloop/sessions/thread-1.json"), JSON.stringify({runtime_session_id:"99999999-2222-3333-4444-555555555555",alias:"brief-2",title_sent:true}));
  const { manager } = makeManager();
  try {
    await manager.sendMessage({text:"hello"});
    assert.equal(requests.filter((r) => r.url.endsWith("/session-start")).length, 1);
    assert.equal(requests[0].body.alias, "brief");
    assert.equal(requests[0].body.parent_session_id, process.env.PRELOOP_PARENT_SESSION_ID);
    const file = JSON.parse(await fs.readFile(path.join(home, ".preloop/sessions/thread-1.json"), "utf8"));
    assert.equal(file.alias, "brief-2");
    assert.equal(file.title_sent, true, "preserve the hook first-prompt marker");
  } finally {
    manager.stop(); globalThis.fetch = original.fetch;
    for (const [key, value] of [["HOME", original.home], ["PRELOOP_SESSION_ALIAS", original.alias], ["PRELOOP_PARENT_SESSION_ID", original.parent]]) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    await fs.rm(home, {recursive:true, force:true});
  }
});

test("registers a lazy native id before the first turn completes", async () => {
  const original = {alias:process.env.PRELOOP_SESSION_ALIAS, fetch:globalThis.fetch};
  process.env.PRELOOP_SESSION_ALIAS = "brief-lazy";
  let calls = 0;
  globalThis.fetch = async () => {calls++; return new Response(JSON.stringify({runtime_session_id:"99999999-2222-3333-4444-555555555555",alias:"brief-lazy"}));};
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  let acknowledgeStart;
  const started = new Promise(resolve => {acknowledgeStart=resolve;});
  let id = null;
  const thread = {get id(){return id;}, async run(_text, options){id="lazy-native"; await options.onSessionStarted?.(id); acknowledgeStart(); await gate; return {finalResponse:"done",usage:null};}};
  const manager = new SessionManager(baseConfig, () => ({startThread:()=>thread, resumeThread:()=>thread}));
  const running = manager.sendMessage({text:"hello"});
  try {
    await started;
    assert.equal(calls,1,"alias must be registered while the worker is still running");
  } finally {
    release(); await running; manager.stop(); globalThis.fetch=original.fetch;
    if(original.alias===undefined) delete process.env.PRELOOP_SESSION_ALIAS; else process.env.PRELOOP_SESSION_ALIAS=original.alias;
  }
});

test("execution-scoped workers register with the execution credential", async () => {
  const original = {alias:process.env.PRELOOP_SESSION_ALIAS, fetch:globalThis.fetch};
  process.env.PRELOOP_SESSION_ALIAS = "brief-execution";
  let authorization;
  globalThis.fetch = async (_url, options) => {
    authorization = options.headers.Authorization;
    return new Response(JSON.stringify({runtime_session_id:"99999999-2222-3333-4444-555555555555",alias:"brief-execution"}));
  };
  const {manager} = makeManager();
  try {
    await manager.sendMessage({text:"hello", startNewSession:true, metadata:{gateway:{api_url:"https://example.preloop.ai",base_url:"https://example.preloop.ai/openai/v1",api_key:"flow_synthetic",model:"gpt-test"}}});
    assert.equal(authorization,"Bearer flow_synthetic");
  } finally {
    manager.stop(); globalThis.fetch=original.fetch;
    if(original.alias===undefined) delete process.env.PRELOOP_SESSION_ALIAS; else process.env.PRELOOP_SESSION_ALIAS=original.alias;
  }
});


test("a finished turn records its end time and no turn text (#1889)", async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({url: String(url), body: JSON.parse(options.body)});
    return new Response(null, {status: 204});
  };
  const { manager } = makeManager();
  try {
    await manager.sendMessage({text: "secret plan"});
    await new Promise((resolve) => setImmediate(resolve));
    const events = requests.filter((r) => r.url.endsWith("/api/v1/agents/session-event"));
    assert.equal(events.length, 1);
    assert.equal(events[0].body.hook_event_name, "Stop");
    assert.equal(events[0].body.source, "codex");
    assert.ok(events[0].body.session_id);
    assert.ok(!JSON.stringify(requests).includes("secret plan"));
  } finally {
    manager.stop();
    globalThis.fetch = original;
  }
});
