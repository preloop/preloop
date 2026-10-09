// Runs the same agent without trigger.dev, to check the Preloop side alone:
//   npm run smoke
import { randomUUID } from "node:crypto";
import { runSupportAgent } from "./agent.ts";

const sessionId = `smoke-${randomUUID()}`;
const { reply, usage } = await runSupportAgent(
  {
    ticketId: "T-1001",
    customerMessage:
      "Order A-42 arrived broken, photos attached. Please refund the 49.00 EUR.",
  },
  sessionId,
);
console.log(JSON.stringify({ sessionId, reply, usage }, null, 2));
