import { randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { createInterface } from "node:readline";

export const name = "preloop-conversation-probe";
export const inject = ["agents", "sessions", "agentDefaultModel"];

/** Exercise the published core, without replacing its loop or persistence. */
export function apply(ctx) {
  void run(ctx).catch((error) => {
    console.error(error);
    ctx.appExit(1);
  });
}

async function run(ctx) {
  await ctx.get("loader")?.await();
  const require = createRequire(realpathSync(process.env.PROBE_DSH));
  const { installModelSelection } = await import(
    require.resolve("@deepseek-ai/dsh-agent")
  );
  const selection = ctx.agentDefaultModel.currentSelection();
  const options = {
    agentOptions: { provider: selection.provider, model: selection.model },
    setup(agentCtx) {
      installModelSelection(agentCtx, {
        current: selection,
        assembled: undefined,
      });
    },
  };
  const handle = process.env.PROBE_SESSION
    ? await ctx.agents.resume({
        ...options,
        resumeSessionId: process.env.PROBE_SESSION,
      })
    : await ctx.agents.create({
        ...options,
        sessionId: `session-${randomUUID()}`,
        meta: { cwd: process.cwd() },
      });
  const { agent } = handle;
  await agent.whenIdle();
  console.log(
    JSON.stringify({ ready: true, session: agent.id, pid: process.pid }),
  );
  ctx.on("agent/assistant-stream", ({ agent: subject, frame }) => {
    if (
      subject === agent &&
      frame.type === "chunk" &&
      frame.chunk.type === "text-delta"
    )
      console.log(JSON.stringify({ delta: frame.chunk.text }));
  });
  for await (const line of createInterface({ input: process.stdin })) {
    const { prompt } = JSON.parse(line);
    const firstSeq = agent.session.seq;
    agent.followup({
      id: randomUUID(),
      role: "user",
      source: { kind: "user" },
      content: [{ type: "text", text: prompt }],
    });
    await agent.whenIdle();
    await ctx.sessions.flush(agent.session);
    let answer = "";
    let reason;
    for (let seq = firstSeq; seq < agent.session.seq; seq++) {
      const event = agent.session.eventAt(seq);
      if (event.type === "assistant/message")
        answer = event.data.message.content
          .filter((b) => b.type === "text")
          .map((b) => b.text)
          .join("");
      if (event.type === "turn/end") reason = event.data.reason;
    }
    console.log(
      JSON.stringify({ answer, reason, session: agent.id, pid: process.pid }),
    );
    if (reason?.kind !== "completed") throw new Error("Turn did not complete");
  }
  await handle.dispose();
  ctx.appExit(0);
}
