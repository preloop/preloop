// The agent itself, independent of trigger.dev so it can also run as a plain
// Node script (see smoke.ts).
import { stepCountIs, streamText, tool } from "ai";
import { z } from "zod";
import { preloopModel, requestApproval } from "./preloop.ts";

export type SupportTicket = {
  ticketId: string;
  customerMessage: string;
};

export async function runSupportAgent(
  ticket: SupportTicket,
  sessionId: string,
  log: (message: string, data?: Record<string, unknown>) => void = console.log,
) {
  const refund = tool({
    description: "Refund an order. Money leaves the company, so a human approves it.",
    inputSchema: z.object({
      orderId: z.string(),
      amountCents: z.number().int().positive(),
      reason: z.string(),
    }),
    execute: async ({ orderId, amountCents, reason }) => {
      const decision = await requestApproval({
        toolName: "refund",
        toolInput: { orderId, amountCents, ticketId: ticket.ticketId },
        reasoning: reason,
        sessionId,
      });
      log("Preloop approval decision", decision);
      if (!decision.approved) {
        return {
          refunded: false,
          message: decision.timedOut
            ? "Nobody approved the refund in time. Tell the customer a human will follow up."
            : `The refund was declined: ${decision.reason || "no reason given"}.`,
        };
      }
      // Call your payment provider here. The example only pretends.
      return { refunded: true, orderId, amountCents };
    },
  });

  const result = streamText({
    model: preloopModel(sessionId),
    system:
      "You are a support agent. Use the refund tool only when the customer " +
      "clearly qualifies, then write a short reply to the customer.",
    prompt: `Ticket ${ticket.ticketId}: ${ticket.customerMessage}`,
    tools: { refund },
    stopWhen: stepCountIs(4),
  });

  let reply = "";
  for await (const chunk of result.textStream) {
    reply += chunk;
  }
  const usage = await result.totalUsage;
  return { reply, usage };
}
