// npm test: requestApproval() must deny whenever Preloop gives no decision.
import assert from "node:assert/strict";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { requestApproval } from "./preloop.ts";

const call = {
  toolName: "refund",
  toolInput: { orderId: "A-42", amountCents: 4900 },
  reasoning: "test",
  sessionId: "test-session",
};

async function withServer(
  handler: http.RequestListener,
  run: (url: string) => Promise<void>,
): Promise<void> {
  const server = http.createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${port}`);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

process.env.PRELOOP_AGENT_TOKEN = "agt_test";

test("denies when Preloop is unreachable", async () => {
  process.env.PRELOOP_URL = "http://127.0.0.1:1";
  const decision = await requestApproval(call);
  assert.equal(decision.approved, false);
  assert.match(decision.reason, /unreachable/);
});

test("denies when the connection is dropped mid-request", async () => {
  await withServer(
    (req) => req.socket.destroy(),
    async (url) => {
      process.env.PRELOOP_URL = url;
      const decision = await requestApproval(call);
      assert.equal(decision.approved, false);
      assert.match(decision.reason, /unreachable/);
    },
  );
});

test("denies on an HTTP error", async () => {
  await withServer(
    (_req, res) => res.writeHead(503).end(),
    async (url) => {
      process.env.PRELOOP_URL = url;
      const decision = await requestApproval(call);
      assert.equal(decision.approved, false);
      assert.match(decision.reason, /HTTP 503/);
    },
  );
});

test("denies on a malformed reply", async () => {
  await withServer(
    (_req, res) => res.writeHead(200, { "content-type": "application/json" }).end("not json"),
    async (url) => {
      process.env.PRELOOP_URL = url;
      const decision = await requestApproval(call);
      assert.equal(decision.approved, false);
      assert.match(decision.reason, /malformed/);
    },
  );
});

test("allows only on an explicit allow", async () => {
  await withServer(
    (_req, res) =>
      res
        .writeHead(200, { "content-type": "application/json" })
        .end(JSON.stringify({ decision: "allow", reason: "ok", request_id: "r1" })),
    async (url) => {
      process.env.PRELOOP_URL = url;
      const decision = await requestApproval(call);
      assert.equal(decision.approved, true);
      assert.equal(decision.requestId, "r1");
    },
  );
});
