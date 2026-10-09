// Two small helpers that connect an AI SDK agent to Preloop:
//   1. preloopModel(): an OpenAI-compatible model whose calls go through the
//      Preloop gateway (budgets, cost per agent, session timeline).
//   2. requestApproval(): asks Preloop for a human decision on a risky tool
//      call (mobile, Slack, Mattermost or webhook) and waits for it.
import { createOpenAI } from "@ai-sdk/openai";

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set (see .env.example)`);
  }
  return value;
}

function preloopUrl(): string {
  return requireEnv("PRELOOP_URL").replace(/\/+$/, "");
}

/**
 * A chat model routed through the Preloop gateway.
 *
 * @param sessionId Groups every model call of one run into one Preloop
 *   runtime session. Use the trigger.dev run id.
 */
export function preloopModel(sessionId: string) {
  const gateway = createOpenAI({
    baseURL: `${preloopUrl()}/openai/v1`,
    apiKey: requireEnv("PRELOOP_AGENT_TOKEN"),
    headers: { "X-Preloop-Session-Id": sessionId },
  });
  // Chat Completions is the most widely supported gateway surface.
  return gateway.chat(process.env.PRELOOP_MODEL ?? "gpt-4o-mini");
}

export type ApprovalDecision = {
  approved: boolean;
  reason: string;
  requestId: string | null;
  timedOut: boolean;
};

type PermissionCheckResponse = {
  decision: "allow" | "deny";
  reason?: string | null;
  request_id?: string | null;
  timed_out?: boolean;
};

/**
 * Ask Preloop whether a tool call may run.
 *
 * Preloop applies your rules for this tool first. When a human has to
 * decide, it creates an approval request, notifies the approvers of the
 * account's approval workflow, and holds this request open until someone
 * decides or the workflow timeout expires (an expiry is a deny).
 */
export async function requestApproval(input: {
  toolName: string;
  toolInput: Record<string, unknown>;
  reasoning: string;
  sessionId: string;
}): Promise<ApprovalDecision> {
  const response = await fetch(`${preloopUrl()}/api/v1/agents/permission-check`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${requireEnv("PRELOOP_AGENT_TOKEN")}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      tool_name: input.toolName,
      tool_input: input.toolInput,
      agent_reasoning: input.reasoning,
      session_id: input.sessionId,
      source: "trigger_dev",
      // Omitted client_decision means "ask": escalate unless a rule decides.
    }),
  });
  if (!response.ok) {
    // Fail closed: no decision from Preloop means the tool does not run.
    return {
      approved: false,
      reason: `Preloop permission check failed with HTTP ${response.status}`,
      requestId: null,
      timedOut: false,
    };
  }
  const body = (await response.json()) as PermissionCheckResponse;
  return {
    approved: body.decision === "allow",
    reason: body.reason ?? "",
    requestId: body.request_id ?? null,
    timedOut: body.timed_out ?? false,
  };
}
