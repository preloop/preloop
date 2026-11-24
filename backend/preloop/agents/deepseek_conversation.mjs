/** Persistent Cordis driver for the pinned dsh 0.1.5-rc.2 core APIs. */
import { randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { createInterface } from "node:readline";

export const name = "preloop-conversation";
export const inject = [
  "agents",
  "sessions",
  "agentDefaultModel",
  "permissionPresets",
];

function emit(event) {
  process.stdout.write(JSON.stringify({ preloop: true, ...event }) + "\n");
}

/** Own one native agent, accepting framed turns and cancellation from stdin. */
export function apply(ctx) {
  void run(ctx).catch(() => {
    // Native exceptions can include requests and credentials.
    emit({ error: true });
    ctx.appExit(1);
  });
}

async function run(ctx) {
  await ctx.get("loader")?.await();
  const require = createRequire(
    realpathSync(process.env.PRELOOP_DSH_EXECUTABLE),
  );
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
  const sessionId = process.env.PRELOOP_DSH_SESSION;
  const handle = sessionId
    ? await ctx.agents.resume({ ...options, resumeSessionId: sessionId })
    : await ctx.agents.create({
        ...options,
        sessionId: `session-${randomUUID()}`,
        meta: { cwd: process.cwd() },
      });
  const { agent } = handle;
  try {
    if (
      agent.session.header.cwd !== process.cwd() ||
      agent.session.header.parentSession
    )
      throw new Error("Incompatible session workspace or ownership");
    await agent.whenIdle();
    // Reapply current hosted policy; historical decisions are audit, not grants.
    ctx.permissionPresets.set(agent.session, "danger-full-access");
    emit({ ready: true, session: agent.id });
    let active = null;
    let turn = null;
    ctx.on("agent/assistant-stream", ({ agent: subject, frame }) => {
      if (
        subject === agent &&
        frame.type === "chunk" &&
        frame.chunk.type === "text-delta"
      )
        emit({ turn, delta: frame.chunk.text });
    });
    async function submit(message) {
      turn = message.turn;
      const firstSeq = agent.session.seq;
      agent.followup({
        id: randomUUID(),
        role: "user",
        source: { kind: "user" },
        content: [{ type: "text", text: message.prompt }],
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
        if (event.type === "turn/end") reason = event.data.reason?.kind;
      }
      emit({ turn, answer, finished: true, reason });
    }
    for await (const line of createInterface({ input: process.stdin })) {
      const message = JSON.parse(line);
      if (message.cancel) {
        agent.cancel({ kind: "user" });
      } else {
        if (active) await active;
        if (
          typeof message.prompt !== "string" ||
          !Number.isInteger(message.turn)
        )
          throw new Error("Invalid turn");
        active = submit(message);
        // Observe failures immediately even while the next input is pending.
        void active.catch(() => {
          emit({ error: true });
          ctx.appExit(1);
        });
      }
    }
    if (active) await active;
    await ctx.sessions.flush(agent.session);
  } finally {
    await handle.dispose();
  }
  ctx.appExit(0);
}
