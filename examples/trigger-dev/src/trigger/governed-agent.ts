import { logger, task } from "@trigger.dev/sdk";
import { runSupportAgent, type SupportTicket } from "../agent.ts";

export const governedSupportAgent = task({
  id: "governed-support-agent",
  run: async (payload: SupportTicket, { ctx }) => {
    // One Preloop runtime session per run: the run id groups every model
    // call and the approval request on the same session timeline.
    const sessionId = ctx.run.id;
    const { reply, usage } = await runSupportAgent(payload, sessionId, (message, data) =>
      logger.info(message, data),
    );
    logger.info("Model usage reported by the AI SDK", { usage });
    return { reply, preloopSessionId: sessionId };
  },
});
