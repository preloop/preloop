// Opt-in, keyless integration test against installed, pinned real runtimes.
// PRELOOP_TEST_RUNTIME_BIN=/path/to/node_modules/.bin node test/runtime-smoke.mjs
import assert from "node:assert/strict";
import http from "node:http";
import { spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

process.env.PRELOOP_DISABLE_TELEMETRY = "true";
process.env.DSH_TELEMETRY_DISABLED = "true";
const bin = process.env.PRELOOP_TEST_RUNTIME_BIN;
if (!bin)
  throw Error(
    "Set PRELOOP_TEST_RUNTIME_BIN to the pinned runtimes bin directory",
  );
const plugin = fileURLToPath(new URL("../", import.meta.url));
const timeoutHelper = fileURLToPath(
  new URL(
    "../../../backend/preloop/agents/harness_model_timeouts.mjs",
    import.meta.url,
  ),
);

async function run(binary, args, env, cwd, input, requireSuccess = true) {
  const child = spawn(binary, args, {
    env,
    cwd,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (data) => {
    output += data;
  });
  child.stderr.on("data", (data) => {
    output += data;
  });
  child.stdin.end(input);
  const timer = setTimeout(() => child.kill("SIGKILL"), 60000);
  const code = await new Promise((resolve, reject) => {
    child.on("exit", resolve);
    child.on("error", reject);
  });
  clearTimeout(timer);
  if (requireSuccess) assert.equal(code, 0, output);
  return output;
}

const runtimes = process.env.PRELOOP_TEST_RUNTIME
  ? [process.env.PRELOOP_TEST_RUNTIME]
  : ["pi", "deepseek"];
assert.ok(runtimes.every((runtime) => ["pi", "deepseek"].includes(runtime)));

for (const runtime of runtimes) {
  const root = mkdtempSync(join(tmpdir(), "preloop-harness-smoke-"));
  let approvals = 0,
    results = 0,
    sessionHeader,
    noteSession,
    noteDelivered = false,
    noteSeen = false,
    noteEnded = false;
  const server = http.createServer(async (req, res) => {
    let text = "";
    for await (const chunk of req) text += chunk;
    const body = JSON.parse(text);
    if (req.url === "/api/v1/agents/session-start") {
      assert.equal(body.source, "deepseek");
      assert.ok(body.session_id);
      noteSession = body.session_id;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ runtime_session_id: "synthetic-runtime" }));
      return;
    }
    if (req.url === "/api/v1/agents/notes/pending") {
      assert.equal(body.session_id, noteSession);
      assert.equal(body.defer_delivery, true);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify(
          noteDelivered
            ? { notes: [], text: null }
            : {
                notes: [{ note_id: "synthetic-note", envelope: {} }],
                text: "<operator-notes>synthetic-note: verify the denial</operator-notes>",
              },
        ),
      );
      return;
    }
    if (req.url === "/api/v1/agents/notes/ack") {
      assert.equal(body.session_id, noteSession);
      assert.deepEqual(body.note_ids, ["synthetic-note"]);
      noteDelivered = true;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end("{}");
      return;
    }
    if (req.url === "/api/v1/agents/session-end") {
      assert.equal(body.external_session_id, noteSession);
      noteEnded = true;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end("{}");
      return;
    }
    if (req.url === "/api/v1/agents/permission-check") {
      approvals++;
      assert.equal(body.source, runtime);
      assert.equal(body.tool_name, "Bash");
      assert.ok(body.session_id);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({ decision: "deny", reason: "smoke-policy-denial" }),
      );
      return;
    }
    sessionHeader ||= req.headers["x-preloop-session-id"];
    if (runtime === "deepseek" && body.tools?.length) {
      const noteMessages = body.messages.filter((message) =>
        JSON.stringify(message).includes("synthetic-note: verify the denial"),
      );
      assert.equal(
        noteMessages.length,
        1,
        "the native log must include the note once",
      );
      assert.equal(noteMessages[0].role, "user");
      assert.ok(
        noteDelivered,
        "receipt acknowledges admitted history before model transport",
      );
      noteSeen = true;
    }
    const toolResult = body.messages?.find(
      (message) => message.role === "tool",
    );
    if (toolResult) {
      assert.match(JSON.stringify(toolResult), /smoke-policy-denial/);
      results++;
    }
    const delta =
      body.tools?.length && !toolResult
        ? {
            role: "assistant",
            tool_calls: [
              {
                index: 0,
                id: "call-smoke",
                type: "function",
                function: {
                  name: "bash",
                  arguments: JSON.stringify({
                    command: `touch ${join(root, "must-not-exist")}`,
                  }),
                },
              },
            ],
          }
        : { role: "assistant", content: "Harness smoke passed" };
    // Exercise first-byte and streaming waits through the actual runtime SDK.
    await new Promise((resolve) => setTimeout(resolve, 100));
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const base = {
      id: "smoke",
      object: "chat.completion.chunk",
      created: 1,
      model: "test",
    };
    res.write(
      "data: " +
        JSON.stringify({
          ...base,
          choices: [{ index: 0, delta, finish_reason: null }],
        }) +
        "\n\n",
    );
    await new Promise((resolve) => setTimeout(resolve, 100));
    res.end(
      "data: " +
        JSON.stringify({
          ...base,
          choices: [
            {
              index: 0,
              delta: {},
              finish_reason: delta.tool_calls ? "tool_calls" : "stop",
            },
          ],
        }) +
        "\n\ndata: [DONE]\n\n",
    );
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const api = `http://127.0.0.1:${server.address().port}`;
    const env = {
      ...process.env,
      PRELOOP_HARNESS: runtime,
      PRELOOP_HARNESS_BIN: join(bin, runtime === "pi" ? "pi" : "dsh"),
      PRELOOP_URL: api,
      PRELOOP_NATIVE_APPROVALS: "on",
      PRELOOP_API_URL: api,
      PRELOOP_API_TOKEN: "test-token",
      PRELOOP_MODEL_TOKEN: "test",
      PI_CODING_AGENT_DIR: root,
      DSH_HOME: root,
      PRELOOP_HARNESS_MODEL: JSON.stringify({
        api: "openai-completions",
        baseUrl: "${PRELOOP_URL}/v1",
        models: [{ id: "test" }],
        timeoutMs: 5000,
      }),
    };
    await run(process.execPath, [join(plugin, "bootstrap.mjs")], env, root, "");
    if (runtime === "pi") {
      const extension = join(root, "extensions", "preloop");
      mkdirSync(extension, { recursive: true });
      writeFileSync(
        join(extension, "index.ts"),
        `export { default } from ${JSON.stringify(pathToFileURL(join(plugin, "pi.mjs")).href)};\n`,
      );
    } else {
      writeFileSync(
        join(root, "settings.yaml"),
        "agent-default-model:\n  provider: deepseek\n  model: native-model\n",
      );
    }
    const args =
      runtime === "pi"
        ? ["--print", "--mode", "json"]
        : ["--profile", "headless"];
    if (runtime === "pi") {
      // A negative control proves the pinned SDK consumes the helper's settings.
      writeFileSync(
        join(root, "settings.json"),
        JSON.stringify({
          retry: { enabled: false, provider: { maxRetries: 0 } },
        }),
      );
      const shortEnv = {
        ...env,
        PRELOOP_HARNESS_MODEL: JSON.stringify({
          ...JSON.parse(env.PRELOOP_HARNESS_MODEL),
          timeoutMs: 20,
        }),
      };
      await run(process.execPath, [timeoutHelper], shortEnv, root, "");
      const timedOut = await run(
        env.PRELOOP_HARNESS_BIN,
        args,
        shortEnv,
        root,
        "Run the requested command",
        false,
      );
      assert.match(timedOut, /timed out|timeout/i);
      assert.doesNotMatch(timedOut, /Harness smoke passed/);
      assert.equal(approvals, 0);
    }
    await run(process.execPath, [timeoutHelper], env, root, "");
    const output = await run(
      join(bin, runtime === "pi" ? "pi" : "dsh"),
      args,
      env,
      root,
      "Run the requested command",
    );
    assert.match(output, /Harness smoke passed/);
    assert.equal(approvals, 1, output);
    assert.equal(results, 1, output);
    assert.equal(existsSync(join(root, "must-not-exist")), false);
    if (runtime === "pi") assert.ok(sessionHeader);
    else {
      assert.ok(noteSeen && noteDelivered, "native model-input note receipt");
      assert.ok(noteEnded, "native session disposal reports authoritative end");
    }
    console.log(
      `${runtime}: real runtime, delayed model headers/chunks, native approval denial and clean exit passed`,
    );
  } finally {
    server.close();
    rmSync(root, { recursive: true, force: true });
  }
}
