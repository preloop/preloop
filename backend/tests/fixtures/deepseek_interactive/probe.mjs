/** Run with: node probe.mjs /absolute/path/to/node_modules/.bin/dsh. */
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

process.env.PRELOOP_DISABLE_TELEMETRY = "true";
process.env.DSH_TELEMETRY_DISABLED = "true";
const dsh = resolve(process.argv[2]);
const root = await mkdtemp(join(tmpdir(), "dsh-conversation-probe-"));
const workspace = join(root, "workspace");
const home = join(root, "home");
await mkdir(workspace);
await mkdir(home);
execFileSync("git", ["init", "--quiet", workspace]);
await writeFile(
  join(workspace, "README.md"),
  "Synthetic conversation workspace.\n",
);
const requests = [];
const server = createServer(async (req, res) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks));
  requests.push(body);
  const users = body.messages.filter((m) => m.role === "user");
  const answer = JSON.stringify({
    first: users[0].content,
    turns: users.length,
  });
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const event = (delta, finish_reason = null) =>
    res.write(
      `data: ${JSON.stringify({
        id: "chatcmpl-synthetic",
        object: "chat.completion.chunk",
        created: 1,
        model: "synthetic",
        choices: [{ index: 0, delta, finish_reason }],
      })}\n\n`,
    );
  event({ role: "assistant", content: answer });
  event({}, "stop");
  res.end("data: [DONE]\n\n");
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
await writeFile(
  join(home, "cordis.patch.yml"),
  JSON.stringify([
    { id: "headless-startup", disabled: true },
    { id: "headless-runner", disabled: true },
    {
      id: "session-persistence-jsonl",
      config: { root: join(home, "sessions"), compression: "none" },
    },
    { id: "sandbox-policy", config: { mode: "danger-full-access" } },
    { id: "permission", config: { defaultPreset: "danger-full-access" } },
    {
      id: "llm-pi-ai",
      config: {
        providers: {
          probe: {
            api: "openai-completions",
            baseURL: `http://127.0.0.1:${server.address().port}/v1`,
            apiKeyEnv: "PROBE_MODEL_TOKEN",
            models: [{ id: "synthetic" }],
          },
        },
      },
    },
    {
      id: "agent-default-model",
      config: { provider: "probe", model: "synthetic" },
    },
    {
      insert: [
        {
          id: "conversation-probe",
          name: fileURLToPath(
            new URL(
              process.env.PROBE_PRODUCTION
                ? "../../../preloop/agents/deepseek_conversation.mjs"
                : "driver.mjs",
              import.meta.url,
            ),
          ),
        },
      ],
    },
  ]),
);

async function launch(session, prompts) {
  const started = performance.now();
  const child = spawn(dsh, ["--profile", "headless"], {
    cwd: workspace,
    env: {
      ...process.env,
      DSH_HOME: home,
      HOME: home,
      PROBE_DSH: dsh,
      PRELOOP_DSH_EXECUTABLE: dsh,
      PRELOOP_DSH_SESSION: session || "",
      PROBE_SESSION: session || "",
      PROBE_MODEL_TOKEN: "synthetic-only",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const deadline = setTimeout(() => child.kill("SIGKILL"), 30000);
  const exited = new Promise((done) => child.on("exit", done));
  child.stderr.pipe(process.stderr);
  const outcomes = [];
  let turnStart = started;
  let firstToken;
  for await (const line of createInterface({ input: child.stdout })) {
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      continue;
    }
    if (event.ready) {
      session = event.session;
      child.stdin.write(JSON.stringify({ prompt: prompts[0], turn: 1 }) + "\n");
    }
    if (event.delta && firstToken === undefined)
      firstToken = performance.now() - turnStart;
    if (event.answer !== undefined) {
      assert.equal(event.reason?.kind || event.reason, "completed");
      outcomes.push({
        ...event,
        pid: child.pid,
        latency_ms: performance.now() - turnStart,
        first_token_ms: firstToken,
      });
      if (outcomes.length === prompts.length) child.stdin.end();
      else {
        turnStart = performance.now();
        firstToken = undefined;
        child.stdin.write(
          JSON.stringify({
            prompt: prompts[outcomes.length],
            turn: outcomes.length + 1,
          }) + "\n",
        );
      }
    }
  }
  const code = await exited;
  clearTimeout(deadline);
  assert.equal(code, 0);
  assert.equal(outcomes.length, prompts.length);
  return { session, outcomes };
}

try {
  const live = await launch(null, [
    "Remember amber-cobalt-37.",
    "Second turn.",
    "Third turn.",
  ]);
  const resumed = await launch(live.session, ["Recall the first turn."]);
  assert.equal(new Set(live.outcomes.map((o) => o.pid)).size, 1);
  assert.notEqual(live.outcomes[0].pid, resumed.outcomes[0].pid);
  const turns = requests.filter((r) =>
    r.messages.some((m) => m.content === "Remember amber-cobalt-37."),
  );
  assert.equal(turns.length, 4);
  for (const [index, request] of turns.entries()) {
    const users = request.messages.filter(
      (m) =>
        m.role === "user" && !m.content.startsWith("Current runtime context."),
    );
    assert.equal(users.length, index + 1);
    assert.match(JSON.stringify(users[0]), /amber-cobalt-37/);
  }
  console.log(
    JSON.stringify({ upstream: "synthetic", live, resumed }, null, 2),
  );
} finally {
  server.closeAllConnections();
  server.close();
  if (process.env.PROBE_KEEP) console.log(root);
  else await rm(root, { recursive: true, force: true });
}
